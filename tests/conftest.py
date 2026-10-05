import sys
import types


class FakeFlask:
    def __init__(self, *args, **kwargs):
        pass

    def get(self, *args, **kwargs):
        def decorator(func):
            return func
        return decorator

    def run(self, *args, **kwargs):
        pass


class FakeResponse:
    def __init__(self, response=None, status=200, mimetype=None):
        self.response = response
        self.status_code = status
        self.mimetype = mimetype


flask = types.ModuleType("flask")
flask.Flask = FakeFlask
flask.Response = FakeResponse
sys.modules.setdefault("flask", flask)
