"""Private service binding only. The HTTP handler exposes no model or data."""

from workers import WorkerEntrypoint, Response


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return Response("Not found", status=404)

    async def analyze(self, payload):
        from hr_platform.cloud_model import execute

        return execute(payload)
