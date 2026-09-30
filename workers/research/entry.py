"""Private service binding only. The HTTP handler exposes no model or data."""

from workers import WorkerEntrypoint, Response


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response("Not found", status=404)

    async def analyze(self, payload):
        from hr_platform.cloud_model import execute

        return execute(payload)

    async def odds(self, payload):
        import json
        from hr_platform.cloud_history import CloudHistory

        try:
            if not isinstance(payload, str) or len(payload) > 4096:
                raise ValueError("INPUT_LIMIT")
            request = json.loads(payload)
            if not isinstance(request, dict):
                raise ValueError("INPUT_SCHEMA")
            history = CloudHistory(self.env.RAW, self.env.INDEX,
                                   storage_policy=json.loads(self.env.STORAGE_POLICY_JSON))
            operation = request.pop("operation")
            if operation not in {"normalize", "history", "asof"}:
                raise ValueError("OPERATION")
            result = await getattr(history, operation)(**request)
            return json.dumps({"status": "OK", "result": result}, allow_nan=False)
        except (ValueError, TypeError, KeyError):
            return json.dumps({"status": "INPUT_OR_DATA_ERROR"})
        except Exception:
            # Storage/FFI failures must not leak original bytes or scientific traces.
            return json.dumps({"status": "STORAGE_OR_RUNTIME_ERROR"})
