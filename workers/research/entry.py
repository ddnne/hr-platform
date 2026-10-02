"""Private service binding only. The HTTP handler exposes no model or data."""

from workers import DurableObject, WorkerEntrypoint, Response


def paper_engine(env):
    import json
    from hr_platform.cloud_paper import CloudPaper
    return CloudPaper(env.RAW, env.INDEX,
        storage_policy=json.loads(env.STORAGE_POLICY_JSON),
        paper_policy=json.loads(env.PAPER_POLICY_JSON))


class PaperClock(DurableObject):
    """One persistent timer for the next existing Paper plan; no source requests."""

    def __init__(self, ctx, env):
        super().__init__(ctx, env)
        import asyncio
        self.lock = asyncio.Lock()
        self.running = False

    async def sync(self):
        async with self.lock:
            # The running handler re-reads the schedule before deciding and
            # re-arms afterwards. Do not duplicate its short wait.
            if self.running:
                return
            at = await paper_engine(self.env).next_alarm() if self.env.PAPER_ENABLED == 'true' else None
            if at is None:
                await self.ctx.storage.deleteAlarm()
            elif await self.ctx.storage.getAlarm() != at:
                await self.ctx.storage.setAlarm(at)

    async def alarm(self, info=None):
        if self.env.PAPER_ENABLED != 'true':
            await self.sync()
            return
        self.running = True
        try:
            await paper_engine(self.env).tick(wait_for_due=True)
        finally:
            self.running = False
        # On failure, leave retries to the platform and the existing Cron.
        await self.sync()


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response("Not found", status=404)

    async def scheduled(self, controller, env=None, ctx=None):
        import asyncio
        import json
        tasks, names = [], []
        if self.env.NORMALIZATION_ENABLED == "true":
            tasks.append(self._normalize_saved())
            names.append('normalizer')
        if self.env.PAPER_ENABLED == "true":
            paper = paper_engine(self.env)
            tasks.append(self._paper_job(paper.tick()))
            names.append('paper')
            if self.env.AUTO_PAPER_ENABLED == "true":
                from hr_platform.cloud_paper_schedule import schedule_day
                tasks.append(self._paper_job(schedule_day(paper, self.env.COLLECTION,
                    json.loads(self.env.PAPER_BASE_CONFIG_JSON),
                    json.loads(self.env.PAPER_SCHEDULE_POLICY_JSON),
                    json.loads(self.env.COLLECTION_POLICY_JSON))))
                names.append('paper_enrollment')
        # One task's parsing/model failure does not cancel the other task.
        for name, result in zip(names, await asyncio.gather(*tasks, return_exceptions=True)):
            if isinstance(result, BaseException):
                print(json.dumps({'component': name, 'status': 'STORAGE_OR_RUNTIME_ERROR'}))

    async def _paper_job(self, task):
        try:
            return await task
        finally:
            await self.env.PAPER_CLOCK.getByName('paper').sync()

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
        result = await self._stored(CloudPaper, payload, operations,
                                    paper_policy=json.loads(self.env.PAPER_POLICY_JSON))
        if (self.env.PAPER_ENABLED == 'true' and json.loads(result)['status'] == 'OK'
                and json.loads(payload)['operation'] == 'enroll'):
            await self.env.PAPER_CLOCK.getByName('paper').sync()
        return result

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
