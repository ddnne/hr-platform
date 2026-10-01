"""Private service binding only. The HTTP handler exposes no model or data."""

from workers import WorkerEntrypoint, Response


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response("Not found", status=404)

    async def scheduled(self, controller, env=None, ctx=None):
        import json
        from hr_platform.cloud_normalization import normalize_next
        if self.env.NORMALIZATION_ENABLED != "true":
            return
        try:
            policy = json.loads(self.env.COLLECTION_POLICY_JSON)
            await normalize_next(self.env.RAW, self.env.INDEX,
                                 json.loads(self.env.STORAGE_POLICY_JSON),
                                 policy['normalization_lease_seconds'])
        except Exception:
            print(json.dumps({'component': 'normalizer', 'status': 'STORAGE_OR_RUNTIME_ERROR'}))

    async def analyze(self, payload):
        from hr_platform.cloud_model import execute

        return execute(payload)

    async def odds(self, payload):
        from hr_platform.cloud_history import CloudHistory
        return await self._stored(CloudHistory, payload, {'normalize', 'history', 'asof'})

    async def races(self, payload):
        from hr_platform.cloud_race_files import CloudRaceFiles
        return await self._stored(CloudRaceFiles, payload, {'normalize', 'history', 'day'})

    async def _stored(self, storage, payload, operations):
        import json

        try:
            if not isinstance(payload, str) or len(payload) > 4096:
                raise ValueError("INPUT_LIMIT")
            request = json.loads(payload)
            if not isinstance(request, dict):
                raise ValueError("INPUT_SCHEMA")
            history = storage(self.env.RAW, self.env.INDEX,
                              storage_policy=json.loads(self.env.STORAGE_POLICY_JSON))
            operation = request.pop("operation")
            if operation not in operations:
                raise ValueError("OPERATION")
            result = await getattr(history, operation)(**request)
            return json.dumps({"status": "OK", "result": result}, allow_nan=False)
        except (ValueError, TypeError, KeyError):
            return json.dumps({"status": "INPUT_OR_DATA_ERROR"})
        except Exception:
            # Storage/FFI failures must not leak original bytes or scientific traces.
            return json.dumps({"status": "STORAGE_OR_RUNTIME_ERROR"})
