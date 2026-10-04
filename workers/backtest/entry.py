"""Private historical research queue. Collectors and native Paper are separate."""
from workers import WorkerEntrypoint, Response


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response('Not found', status=404)

    def engine(self):
        import json
        from hr_platform.cloud_research import CloudResearch
        return CloudResearch(self.env.RAW, self.env.INDEX,
            storage_policy=json.loads(self.env.STORAGE_POLICY_JSON),
            research_policy=json.loads(self.env.RESEARCH_POLICY_JSON), engine_id=self.env.ENGINE_ID)

    async def scheduled(self, controller, env=None, ctx=None):
        import asyncio
        if self.env.RESEARCH_ENABLED == 'true':
            engine = self.engine()
            tasks = [engine.tick()]
            if getattr(self.env, 'AUTO_RESEARCH_ENABLED', 'false') == 'true':
                tasks.extend([engine.schedule_registered(), engine.refresh_evaluations()])
            for result in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(result, BaseException):
                    print('{"component":"research_queue","status":"STORAGE_OR_RUNTIME_ERROR"}')

    async def research(self, payload):
        import json
        try:
            engine = self.engine()
            if not isinstance(payload, str) or len(payload.encode()) > engine.policy['max_rpc_bytes']:
                raise ValueError('INPUT_LIMIT')
            request = json.loads(payload)
            operation = request.pop('operation')
            if operation not in {'register', 'activate', 'enqueue', 'jobs', 'result', 'final_prices', 'evaluate', 'evaluation'}:
                raise ValueError('OPERATION')
            result = await getattr(engine, operation)(**request)
            return json.dumps({'status': 'OK', 'result': result}, allow_nan=False)
        except (ValueError, KeyError, TypeError):
            return json.dumps({'status': 'INPUT_OR_DATA_ERROR'})
        except Exception:
            return json.dumps({'status': 'STORAGE_OR_RUNTIME_ERROR'})
