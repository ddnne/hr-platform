"""Private service binding only. The HTTP handler exposes no model or data."""

from workers import WorkerEntrypoint, Response


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response("Not found", status=404)

    async def scheduled(self, controller, env=None, ctx=None):
        import asyncio
        import json
        from hr_platform.cloud_paper import CloudPaper
        tasks, names = [], []
        if self.env.NORMALIZATION_ENABLED == "true":
            tasks.append(self._normalize_saved())
            names.append('normalizer')
        if self.env.PAPER_ENABLED == "true":
            paper = CloudPaper(self.env.RAW, self.env.INDEX,
                storage_policy=json.loads(self.env.STORAGE_POLICY_JSON),
                paper_policy=json.loads(self.env.PAPER_POLICY_JSON))
            tasks.append(paper.tick(wait_for_due=True))
            names.append('paper')
            if self.env.AUTO_PAPER_ENABLED == "true":
                from hr_platform.cloud_paper_schedule import schedule_day
                tasks.append(schedule_day(paper, self.env.COLLECTION,
                    json.loads(self.env.PAPER_BASE_CONFIG_JSON),
                    json.loads(self.env.PAPER_SCHEDULE_POLICY_JSON),
                    json.loads(self.env.COLLECTION_POLICY_JSON)))
                names.append('paper_enrollment')
        # One task's parsing/model failure does not cancel the other task.
        for name, result in zip(names, await asyncio.gather(*tasks, return_exceptions=True)):
            if isinstance(result, BaseException):
                print(json.dumps({'component': name, 'status': 'STORAGE_OR_RUNTIME_ERROR'}))

    async def _normalize_saved(self):
        import json
        from hr_platform.cloud_normalization import normalize_next
        policy = json.loads(self.env.COLLECTION_POLICY_JSON)
        return await normalize_next(self.env.RAW, self.env.INDEX,
            json.loads(self.env.STORAGE_POLICY_JSON), policy['normalization_lease_seconds'])

    async def normalize_saved(self):
        """Private wakeup after capture. Shares the Cron lease; never runs Paper."""
        import json
        if self.env.NORMALIZATION_ENABLED != "true":
            return json.dumps({'status': 'DISABLED'})
        try:
            return json.dumps(await self._normalize_saved())
        except Exception:
            return json.dumps({'status': 'STORAGE_OR_RUNTIME_ERROR'})

    async def analyze(self, payload):
        from hr_platform.cloud_model import execute

        return execute(payload)

    async def odds(self, payload):
        from hr_platform.cloud_history import CloudHistory
        return await self._stored(CloudHistory, payload, {'normalize', 'history', 'asof'})

    async def races(self, payload):
        from hr_platform.cloud_race_files import CloudRaceFiles
        return await self._stored(CloudRaceFiles, payload, {'normalize', 'history', 'day', 'schedules'})

    async def pages(self, payload):
        from hr_platform.cloud_pages import CloudPages
        return await self._stored(CloudPages, payload, {'normalize', 'history', 'asof'})

    async def paper(self, payload):
        import json
        from hr_platform.cloud_paper import CloudPaper
        operations = {'enroll', 'history'} if self.env.PAPER_ENABLED == "true" else {'history'}
        return await self._stored(CloudPaper, payload, operations,
                                 paper_policy=json.loads(self.env.PAPER_POLICY_JSON))

    async def _stored(self, storage, payload, operations, **options):
        import json

        try:
            if not isinstance(payload, str) or len(payload) > 4096:
                raise ValueError("INPUT_LIMIT")
            request = json.loads(payload)
            if not isinstance(request, dict):
                raise ValueError("INPUT_SCHEMA")
            history = storage(self.env.RAW, self.env.INDEX,
                              storage_policy=json.loads(self.env.STORAGE_POLICY_JSON), **options)
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
