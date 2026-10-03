"""Real SQLite queries with a fake asynchronous D1/R2 binding, not a cloud measurement."""
import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from hr_platform import fixtures as f
from hr_platform import cloud_history as module
from hr_platform.cloud_history import CloudHistory
from hr_platform.common import sha, stamp


class Statement:
    def __init__(self, db, sql):
        self.db, self.sql, self.args = db, sql, ()

    def bind(self, *args):
        self.args = args
        return self

    async def first(self):
        row = self.db.conn.execute(self.sql, self.args).fetchone()
        return dict(row) if row else None

    async def all(self):
        return {'results': [dict(r) for r in self.db.conn.execute(self.sql, self.args)]}

    async def run(self):
        if self.db.fail and self.sql.startswith(self.db.fail):
            raise RuntimeError('injected write failure')
        if self.db.before_publish and self.sql.startswith('UPDATE odds_parses'):
            self.db.before_publish()
        self.db.conn.execute(self.sql, self.args)
        self.db.conn.commit()


class Database:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA foreign_keys=ON')
        self.fail = None
        self.before_publish = None
        self.clock = lambda: f.at(5)
        # D1 evaluates this clock at statement execution, after request delay.
        self.conn.create_function('strftime', 2, lambda *_: stamp(self.clock()))

    def prepare(self, sql):
        return Statement(self, sql)


class Object:
    def __init__(self, data):
        self.data, self.size = data, len(data)

    async def arrayBuffer(self):
        return self.data


class Bucket:
    def __init__(self):
        self.objects, self.writes = {}, 0
        self.normalized_reads = 0

    async def get(self, key):
        self.normalized_reads += key.startswith('odds-normalized/')
        return Object(self.objects[key]) if key in self.objects else None

    async def head(self, key):
        return await self.get(key)

    async def put(self, key, text):
        self.objects[key] = text.encode()
        self.writes += 1


@pytest.fixture
def cloud(tmp_path):
    db, bucket = Database(tmp_path / 'cloud.sqlite'), Bucket()
    for p in sorted(Path('migrations').glob('*.sql')):
        db.conn.executescript(p.read_text())
    clock = [f.at(5)]
    db.clock = lambda: clock[0]
    h = CloudHistory(bucket, db, lambda: clock[0])
    def seed(number, minute, raw=None, kind='SYNTHETIC', source='nar-daily-odds'):
        raw = f.archive() if raw is None else raw
        event = f'{source}:{number}'
        received, saved = stamp(f.at(minute)), stamp(f.at(minute, 1))
        db.conn.execute('INSERT INTO captures(event_id,scheduled_capture_at,status) VALUES(?,?,?)',
                        (event, received, 'SYNTHETIC_FIXTURE'))
        db.conn.execute('INSERT INTO raw_observations VALUES(?,?,?,?,?,?,NULL,NULL)',
                        (event, sha(raw), received, saved, 'body_200', kind))
        db.conn.commit()
        bucket.objects[f'raw/{sha(raw)}'] = raw
        return event
    yield SimpleNamespace(h=h, db=db, bucket=bucket, clock=clock, seed=seed, path=tmp_path / 'cloud.sqlite')
    db.conn.close()


def test_same_body_observations_replay_and_reopen(cloud):
    async def scenario():
        one, two = cloud.seed(1, 0), cloud.seed(2, 2)
        p1 = await cloud.h.normalize(one)
        assert await cloud.h.normalize(one) == p1
        cloud.clock[0] = f.at(6)
        p2 = await cloud.h.normalize(two)
        assert p1['body_hash'] == p2['body_hash'] and p1['parse_id'] != p2['parse_id']
        assert cloud.bucket.writes == 2
        assert not (await cloud.h.history(f.RACE, f.at(4)))['history']
        assert len((await cloud.h.history(f.RACE, f.at(5)))['history']) == 1
        view = await cloud.h.asof(f.RACE, ['win', 'exacta', 'quinella'], f.at(6))
        assert all(v['observation_id'] == two for v in view['markets'].values())
        assert view['gaps'] is None and view['gap_coverage'] == 'UNQUALIFIED_CAPTURE_PLAN'
        assert not view['paper_eligible'] and view['reason'] is None
        assert all(v['content']['state']['status'] == 'UNKNOWN' for v in view['markets'].values())
        assert all(v['content']['source_updated_at'] is None for v in view['markets'].values())
        db = Database(cloud.path)
        try:
            reopened = CloudHistory(cloud.bucket, db, cloud.h.clock)
            assert await reopened.asof(f.RACE, ['win', 'exacta', 'quinella'], f.at(6)) == view
        finally:
            db.conn.close()
    asyncio.run(scenario())


def test_reparse_cannot_backdate_or_replace_newer_receipt(cloud, monkeypatch):
    async def scenario():
        one, two = cloud.seed(1, 0), cloud.seed(2, 2)
        await cloud.h.normalize(one)
        cloud.clock[0] = f.at(6)
        await cloud.h.normalize(two)
        before = await cloud.h.asof(f.RACE, ['quinella'], f.at(6))
        cloud.clock[0] = f.at(7)
        monkeypatch.setattr(module, 'VERSION', 'synthetic-repaired-parser')
        revised = await cloud.h.normalize(one)
        assert revised['available_at'] == stamp(f.at(7))
        assert await cloud.h.asof(f.RACE, ['quinella'], f.at(6)) == before
        after = await cloud.h.asof(f.RACE, ['quinella'], f.at(7))
        assert after['markets']['quinella']['observation_id'] == two
        assert len((await cloud.h.history(f.RACE, f.at(7)))['history']) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['INSERT OR IGNORE INTO odds_races', 'UPDATE odds_parses'])
def test_partial_publication_is_unavailable_until_repair(cloud, fault):
    async def scenario():
        event = cloud.seed(1, 0)
        cloud.db.fail = fault
        with pytest.raises(RuntimeError):
            await cloud.h.normalize(event)
        assert not (await cloud.h.history(f.RACE, f.at(5)))['history']
        cloud.clock[0] = f.at(8)
        cloud.db.fail = None
        row = await cloud.h.normalize(event)
        assert row['available_at'] == stamp(f.at(8))
        assert not (await cloud.h.history(f.RACE, f.at(5)))['history']
        assert len((await cloud.h.history(f.RACE, f.at(8)))['history']) == 1
        assert cloud.db.conn.execute('SELECT count(*) FROM odds_parses').fetchone()[0] == 1
    asyncio.run(scenario())


def test_failure_replay_and_later_success_leave_raw_collection_intact(cloud):
    async def scenario():
        failed = cloud.seed(1, 0, b'PK bad zip with private data')
        bad = await cloud.h.normalize(failed)
        assert bad['status'] == 'ERROR' and bad['available_at'] is None
        assert bad['error_code'] == 'ValueError'
        assert 'private' not in json.dumps(bad)
        assert await cloud.h.normalize(failed) == bad
        good = cloud.seed(2, 2)
        assert (await cloud.h.normalize(good))['status'] == 'OK'
        assert cloud.db.conn.execute('SELECT count(*) FROM raw_observations').fetchone()[0] == 2
        assert {r[0] for r in cloud.db.conn.execute('SELECT status FROM captures')} == {'SYNTHETIC_FIXTURE'}
        assert cloud.db.conn.execute('SELECT blocked FROM source_control').fetchone()[0] == 0
    asyncio.run(scenario())


def test_final_only_is_readable_history_but_not_asof_input(cloud):
    async def scenario():
        await cloud.h.normalize(cloud.seed(1, 0, kind='FINAL_ONLY'))
        assert len((await cloud.h.history(f.RACE, f.at(5)))['history']) == 1
        view = await cloud.h.asof(f.RACE, ['win'], f.at(5))
        assert view['markets'] == {} and view['reason'] == 'DATA_MISSING'
    asyncio.run(scenario())


def test_final_prices_keep_availability_and_do_not_fall_back_to_intermediate(cloud):
    async def scenario():
        await cloud.h.normalize(cloud.seed(61, 0, kind='DAILY_SNAPSHOT'))
        old_input = await cloud.h.asof(f.RACE, ['win', 'quinella'], f.at(5))
        missing = await cloud.h.final_prices(f.RACE, ['win', 'quinella'], f.at(5))
        assert missing['markets'] == {} and missing['missing_markets'] == ['win', 'quinella']
        final_event = cloud.seed(62, 2, f.archive(distorted=False), 'FINAL_ONLY')
        cloud.clock[0] = f.at(8)
        parsed = await cloud.h.normalize(final_event)
        prices = await cloud.h.final_prices(f.RACE, ['win', 'quinella'], f.at(8))
        assert not prices['paper_eligible'] and prices['purpose'] == 'POST_RACE_FINAL_PRICE_EVALUATION'
        assert not prices['missing_markets']
        with pytest.raises(ValueError, match='ASOF_IN_FUTURE'):
            await cloud.h.final_prices(f.RACE, ['win'], f.at(9))
        with pytest.raises(ValueError, match='MARKETS'):
            await cloud.h.final_prices(f.RACE, ['win', 'win'], f.at(8))
        for row in prices['markets'].values():
            assert row['event']['dataset_kind'] == 'FINAL_ONLY'
            assert row['observation_id'] == final_event and row['parse_id'] == parsed['parse_id']
            assert row['available_at'] == parsed['available_at']
            assert row['content']['source_updated_at'] is None
        assert await cloud.h.final_prices(f.RACE, ['win', 'quinella'], f.at(5)) == missing
        assert await cloud.h.asof(f.RACE, ['win', 'quinella'], f.at(5)) == old_input
        assert (await cloud.h.asof(f.RACE, ['quinella'], f.at(8)))['markets']['quinella']['event']['dataset_kind'] == 'DAILY_SNAPSHOT'
        cloud.clock[0] = f.at(9)
        await cloud.h.normalize(cloud.seed(63, 8, kind='DAILY_SNAPSHOT'))
        assert await cloud.h.final_prices(f.RACE, ['win', 'quinella'], f.at(9)) == {**prices, 'asof_at': stamp(f.at(9))}
    asyncio.run(scenario())


def test_final_price_reparse_cannot_restore_removed_market(cloud, monkeypatch):
    async def scenario():
        event = cloud.seed(64, 0, kind='FINAL_ONLY')
        await cloud.h.normalize(event)
        past = await cloud.h.final_prices(f.RACE, ['quinella'], f.at(5))
        original = module.iter_odds_races
        def removed(*args, **kwargs):
            for race, body in original(*args, **kwargs):
                del body['markets']['quinella']
                yield race, body
        monkeypatch.setattr(module, 'iter_odds_races', removed)
        monkeypatch.setattr(module, 'VERSION', 'synthetic-final-price-reparse')
        cloud.clock[0] = f.at(8)
        await cloud.h.normalize(event)
        assert (await cloud.h.final_prices(f.RACE, ['quinella'], f.at(8)))['missing_markets'] == ['quinella']
        assert await cloud.h.final_prices(f.RACE, ['quinella'], f.at(5)) == past
    asyncio.run(scenario())


def test_later_value_never_leaks_into_old_view_and_stale_stays_stale(cloud):
    async def scenario():
        await cloud.h.normalize(cloud.seed(1, 0))
        before = await cloud.h.asof(f.RACE, ['quinella'], f.at(5))
        cloud.clock[0] = f.at(10)
        await cloud.h.normalize(cloud.seed(2, 9, f.archive(distorted=False)))
        assert await cloud.h.asof(f.RACE, ['quinella'], f.at(5)) == before
        after = (await cloud.h.asof(f.RACE, ['quinella'], f.at(10)))['markets']['quinella']
        assert after['observation_id'] != before['markets']['quinella']['observation_id']
        assert after['content']['quotes'] != before['markets']['quinella']['content']['quotes']
        cloud.clock[0] = f.at(20)
        stale = await cloud.h.asof(f.RACE, ['win'], f.at(20))
        assert stale['reason'] == 'STALE' and stale['markets']['win']['age_seconds'] == 660
    asyncio.run(scenario())


def test_corrupt_raw_never_publishes_normalized_data(cloud):
    event = cloud.seed(1, 0)
    key = next(iter(cloud.bucket.objects))
    cloud.bucket.objects[key] = b'wrong content'
    with pytest.raises(ValueError, match='BODY_CORRUPT'):
        asyncio.run(cloud.h.normalize(event))
    assert cloud.db.conn.execute('SELECT count(*) FROM odds_parses').fetchone()[0] == 0


def test_imported_receipt_keeps_a_distinct_observation_identity(cloud):
    event = cloud.seed('a' * 64, 0, source='nar-mac-import')
    result = asyncio.run(cloud.h.normalize(event))
    assert result['observation_id'] == event and result['status'] == 'OK'
    assert asyncio.run(cloud.h.normalize(event)) == result


def test_publication_delay_uses_database_execution_clock(cloud):
    async def scenario():
        event = cloud.seed(1, 0)
        cloud.db.before_publish = lambda: cloud.clock.__setitem__(0, f.at(8))
        parsed = await cloud.h.normalize(event)
        assert parsed['parsed_at'] == stamp(f.at(5))
        assert parsed['available_at'] == stamp(f.at(8))
        assert not (await cloud.h.history(f.RACE, f.at(6)))['history']
    asyncio.run(scenario())


@pytest.mark.parametrize('remove', ['market', 'race'])
def test_reparse_removal_cannot_resurrect_older_version(cloud, monkeypatch, remove):
    async def scenario():
        event = cloud.seed(1, 0)
        await cloud.h.normalize(event)
        before = await cloud.h.asof(f.RACE, ['win', 'quinella'], f.at(5), max_age=600)
        original = module.iter_odds_races
        def corrected(*args):
            races = dict(original(*args))
            if remove == 'market':
                del races[f.RACE]['markets']['quinella']
            else:
                del races[f.RACE]
            return iter(races.items())
        monkeypatch.setattr(module, 'iter_odds_races', corrected)
        monkeypatch.setattr(module, 'VERSION', 'synthetic-correction')
        cloud.clock[0] = f.at(6)
        await cloud.h.normalize(event)
        assert await cloud.h.asof(f.RACE, ['win', 'quinella'], f.at(5), max_age=600) == before
        after = await cloud.h.asof(f.RACE, ['win', 'quinella'], f.at(6), max_age=600)
        assert after['reason'] == 'DATA_MISSING' and 'quinella' not in after['markets']
        if remove == 'race':
            assert not after['markets']
    asyncio.run(scenario())


def test_history_pages_and_asof_read_only_needed_body(cloud):
    async def scenario():
        for i in range(5):
            cloud.clock[0] = f.at(i + 5)
            await cloud.h.normalize(cloud.seed(i, i))
        cloud.bucket.normalized_reads = 0
        view = await cloud.h.asof(f.RACE, ['win', 'quinella', 'exacta'], f.at(9))
        assert all(x['observation_id'] == 'nar-daily-odds:4' for x in view['markets'].values())
        assert cloud.bucket.normalized_reads == 2
        rows, cursor = [], None
        while True:
            page = await cloud.h.history(f.RACE, f.at(9), cursor, limit=2)
            assert len(page['history']) <= 2
            rows.extend(page['history'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        assert len(rows) == len({r['parse_id'] for r in rows}) == 5
    asyncio.run(scenario())


def test_late_csv_error_leaves_written_race_unavailable(cloud):
    import io
    import zipfile
    from hr_platform.parser import unzip

    csv_bytes = next(iter(unzip(f.archive()).values()))
    rows = csv_bytes.splitlines(keepends=True)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        # The first race is saved when the second starts; a later bad row fails.
        archive.writestr('20000101_odds.csv', csv_bytes + rows[1].replace(b'SYNTHETIC', b'NEXT') + b'bad\n')
    async def scenario():
        result = await cloud.h.normalize(cloud.seed(1, 0, raw.getvalue()))
        assert result['status'] == 'ERROR' and result['available_at'] is None
        assert cloud.bucket.writes == 1
        assert not (await cloud.h.history(f.RACE, f.at(5)))['history']
        assert cloud.db.conn.execute('SELECT count(*) FROM odds_races').fetchone()[0] == 0
    asyncio.run(scenario())


def test_legacy_whole_archive_history_is_still_readable(cloud):
    from hr_platform.common import canonical
    from hr_platform.parser import parse_odds

    async def scenario():
        parsed = await cloud.h.normalize(cloud.seed(1, 0))
        expected = parse_odds(f.archive(), {})
        digest = await cloud.h.save_body(canonical(expected))
        cloud.db.conn.execute('UPDATE odds_parses SET body_hash=?', (digest,))
        cloud.db.conn.commit()
        result = await cloud.h.history(f.RACE, parsed['available_at'])
        assert result['history'][0]['content'] == expected[f.RACE]
    asyncio.run(scenario())



def test_history_byte_budget_advances_cursor_without_dropping_rows(cloud, monkeypatch):
    from hr_platform.common import canonical

    async def scenario():
        for i in range(3):
            cloud.clock[0] = f.at(i + 5)
            await cloud.h.normalize(cloud.seed(i, i))
        expected = (await cloud.h.history(f.RACE, f.at(7)))['history']
        monkeypatch.setattr(module, 'HISTORY_PAGE_BYTES', max(len(canonical(x)) for x in expected) + 1)
        result, cursor = [], None
        while True:
            page = await cloud.h.history(f.RACE, f.at(7), cursor)
            assert len(page['history']) == 1
            result.extend(page['history'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        assert result == expected
    asyncio.run(scenario())


def multi_race_archive(count):
    import io
    import zipfile
    from hr_platform.parser import unzip

    rows = next(iter(unzip(f.archive()).values())).splitlines(keepends=True)
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w') as archive:
        data = rows[0] + b''.join(b''.join(rows[1:]).replace(b'SYNTHETIC', f'SYNTHETIC{i}'.encode())
                                  for i in range(count))
        archive.writestr('20000101_odds.csv', data)
    return out.getvalue()


class PendingBucket(Bucket):
    def __init__(self):
        super().__init__()
        self.active = self.peak = 0
        self.fail_first_put = False

    async def get(self, key):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            return await super().get(key)
        finally:
            self.active -= 1

    async def put(self, key, text):
        self.active += 1
        self.peak = max(self.peak, self.active)
        fail, self.fail_first_put = self.fail_first_put, False
        try:
            await asyncio.sleep(0)
            if fail:
                raise RuntimeError('injected R2 failure')
            await super().put(key, text)
        finally:
            self.active -= 1


@pytest.mark.parametrize('byte_budget,peak', [(1048576, 3), (1, 1)])
def test_parallel_storage_keeps_hashes_history_and_batch_limit(cloud, byte_budget, peak):
    async def scenario():
        raw = multi_race_archive(5)
        first = await cloud.h.normalize(cloud.seed(1, 0, raw))
        bucket = PendingBucket()
        bucket.objects = dict(cloud.bucket.objects)
        policy = json.loads(Path('configs/cloud-storage.json').read_text())
        policy['normalization_batch_bytes'] = byte_budget
        h = CloudHistory(bucket, cloud.db, cloud.h.clock, storage_policy=policy)
        second_id = cloud.seed(2, 2, raw)
        cloud.clock[0] = f.at(8)
        second = await h.normalize(second_id)
        assert second['body_hash'] == first['body_hash']
        assert second['available_at'] == stamp(f.at(8))
        assert bucket.peak == peak and bucket.active == 0
        # Five existing race bodies plus their manifest, one GET each, no HEAD+GET.
        assert bucket.normalized_reads == 6 and bucket.writes == 0
        old = await h.asof('20000101:SYNTHETIC0:1', ['win'], f.at(5))
        assert old['markets']['win']['observation_id'] == first['observation_id']
        assert await h.normalize(second_id) == second
    asyncio.run(scenario())


def test_storage_failure_drains_batch_and_never_publishes_partial_data(cloud):
    async def scenario():
        event = cloud.seed(1, 0, multi_race_archive(4))
        bucket = PendingBucket()
        bucket.objects = dict(cloud.bucket.objects)
        bucket.fail_first_put = True
        h = CloudHistory(bucket, cloud.db, cloud.h.clock,
                         storage_policy=json.loads(Path('configs/cloud-storage.json').read_text()))
        with pytest.raises(RuntimeError, match='injected R2 failure'):
            await h.normalize(event)
        assert bucket.active == 0 and bucket.writes == 2
        assert cloud.db.conn.execute('SELECT count(*) FROM odds_parses').fetchone()[0] == 0
        assert not (await h.history('20000101:SYNTHETIC0:1', f.at(5)))['history']
        cloud.clock[0] = f.at(8)
        result = await h.normalize(event)
        assert result['available_at'] == stamp(f.at(8))
        assert not (await h.history('20000101:SYNTHETIC0:1', f.at(5)))['history']
        assert len((await h.history('20000101:SYNTHETIC0:1', f.at(8)))['history']) == 1
    asyncio.run(scenario())


def test_single_read_of_existing_body_still_rejects_corruption(cloud):
    data = b'SYNTHETIC normalized body'
    cloud.bucket.objects[f'odds-normalized/{sha(data)}'] = b'corrupt'
    with pytest.raises(ValueError, match='BODY_CORRUPT'):
        asyncio.run(cloud.h.save_body(data))
    assert cloud.bucket.normalized_reads == 1 and cloud.bucket.writes == 0


@pytest.mark.parametrize('field,value', [('normalization_concurrency', True),
    ('normalization_concurrency', 5), ('normalization_batch_bytes', 0)])
def test_invalid_storage_limits_fail_before_io(cloud, field, value):
    policy = json.loads(Path('configs/cloud-storage.json').read_text())
    policy[field] = value
    with pytest.raises(ValueError, match='STORAGE_POLICY'):
        CloudHistory(cloud.bucket, cloud.db, storage_policy=policy)
    assert cloud.bucket.writes == cloud.bucket.normalized_reads == 0


def test_cloud_race_snapshots_preserve_results_clock_and_reobservations(cloud):
    from hr_platform.cloud_race_files import CloudRaceFiles
    from test_realdata import race_archive

    async def scenario():
        h = CloudRaceFiles(cloud.bucket, cloud.db, lambda: cloud.clock[0])
        one = cloud.seed(1, 0, race_archive(), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        p1 = await h.normalize(one)
        first = await h.day('20000101', f.at(5))
        assert first['races'][f.RACE]['scheduled_start_at'] == '2000-01-01T14:14:00+09:00'
        assert first['races'][f.RACE]['sales_close_at'] is None
        assert not first['races'][f.RACE]['result_present']
        assert not first['paper_eligible']
        assert (await h.day('20000101', f.at(4)))['snapshot'] is None
        cloud.clock[0] = f.at(6)
        two = cloud.seed(2, 2, race_archive(), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        p2 = await h.normalize(two)
        assert p1['body_hash'] == p2['body_hash'] and p1['parse_id'] != p2['parse_id']
        assert await h.normalize(two) == p2
        three = cloud.seed(3, 4, race_archive(finished=True), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        cloud.clock[0] = f.at(7)
        await h.normalize(three)
        assert await h.day('20000101', f.at(5)) == first
        latest = await h.day('20000101', f.at(7))
        assert latest['races'][f.RACE]['result_present']
        assert latest['races'][f.RACE]['status'] == 'UNKNOWN'
        assert not latest['races'][f.RACE]['final']
        history = await h.history(f.RACE, f.at(7))
        assert len(history['history']) == 3
        assert history['history'][0]['content'] == first['races'][f.RACE]
        assert len((await h.history(f.RACE, f.at(7), since=f.at(2)))['history']) == 2
        assert not (await h.history(f.RACE, f.at(5), since=f.at(2)))['history']
    asyncio.run(scenario())


def test_race_parse_failure_does_not_change_odds_or_provider_stop(cloud):
    from hr_platform.cloud_race_files import CloudRaceFiles

    async def scenario():
        h = CloudRaceFiles(cloud.bucket, cloud.db, lambda: cloud.clock[0])
        bad = cloud.seed(1, 0, f.archive(), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        assert (await h.normalize(bad))['status'] == 'ERROR'
        odds = cloud.seed(2, 2)
        assert (await cloud.h.normalize(odds))['status'] == 'OK'
        assert (await h.day('20000101', f.at(5)))['snapshot'] is None
        assert cloud.db.conn.execute("SELECT blocked FROM source_control WHERE source='nar-daily-odds'").fetchone()[0] == 0
    asyncio.run(scenario())


def test_race_filter_reads_only_selected_body_and_does_not_restore_removed_race(cloud):
    import csv
    import io
    import zipfile
    from hr_platform.cloud_race_files import CloudRaceFiles
    from test_realdata import race_archive

    def races(numbers):
        result = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(race_archive())) as source, zipfile.ZipFile(result, 'w') as dest:
            for name in source.namelist():
                reader = csv.DictReader(io.StringIO(source.read(name).decode('utf-8-sig')))
                rows, text = list(reader), io.StringIO()
                writer = csv.DictWriter(text, reader.fieldnames)
                writer.writeheader()
                writer.writerows({**row, 'レース番号': str(n)} for n in numbers for row in rows)
                dest.writestr(name, text.getvalue().encode('utf-8-sig'))
        return result.getvalue()

    async def scenario():
        h = CloudRaceFiles(cloud.bucket, cloud.db, lambda: cloud.clock[0])
        one = cloud.seed(1, 0, races((1, 2)), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        await h.normalize(one)
        full = await h.day('20000101', f.at(5))
        reads, get = [], cloud.bucket.get

        async def record_get(key):
            reads.append(key)
            return await get(key)

        cloud.bucket.get = record_get
        first = await h.day('20000101', f.at(5), race_id=f.RACE)
        assert first == {**full, 'races': {f.RACE: full['races'][f.RACE]}}
        assert len(reads) == 2  # One manifest and the selected race, not the other race.
        assert (await h.day('20000101', f.at(4), race_id=f.RACE))['snapshot'] is None
        cloud.clock[0] = f.at(7)
        two = cloud.seed(2, 6, races((2,)), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        latest = await h.normalize(two)
        removed = await h.day('20000101', f.at(7), race_id=f.RACE)
        assert removed['snapshot']['parse_id'] == latest['parse_id']
        assert removed['races'] == {} and not removed['paper_eligible']
        assert await h.day('20000101', f.at(5), race_id=f.RACE) == first
        with pytest.raises(ValueError, match='RACE_DATE_MISMATCH'):
            await h.day('20000101', f.at(7), race_id='20000102:SYNTHETIC:1')

    asyncio.run(scenario())


def test_separate_normalizer_claims_once_and_preserves_parse_error(cloud):
    from hr_platform.cloud_normalization import normalize_next

    async def scenario():
        cloud.seed(1, 0, kind='DAILY_SNAPSHOT')
        def run():
            return normalize_next(cloud.bucket, cloud.db, None, 60, clock=lambda: cloud.clock[0])
        assert (await run())['status'] == 'PARSED'
        assert (await run())['status'] == 'IDLE'
        cloud.seed(2, 2, f.archive(), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        assert (await run())['status'] == 'PARSE_ERROR'
        assert (await run())['status'] == 'IDLE'
        assert cloud.db.conn.execute("SELECT count(*) FROM normalization_jobs WHERE status='DONE'").fetchone()[0] == 2
    asyncio.run(scenario())


def test_normalizer_storage_failure_retries_only_after_lease(cloud):
    from hr_platform.cloud_normalization import normalize_next

    async def scenario():
        cloud.seed(1, 0, kind='DAILY_SNAPSHOT')
        def run():
            return normalize_next(cloud.bucket, cloud.db, None, 60, clock=lambda: cloud.clock[0])
        put = cloud.bucket.put

        async def failed(*_):
            raise RuntimeError('storage unavailable')

        cloud.bucket.put = failed
        with pytest.raises(RuntimeError, match='storage unavailable'):
            await run()
        cloud.bucket.put = put
        assert (await run())['status'] == 'IDLE'
        cloud.clock[0] = f.at(7)
        assert (await run())['status'] == 'PARSED'
        assert not (await cloud.h.history(f.RACE, f.at(5)))['history']
    asyncio.run(scenario())


def test_normalizer_prioritizes_fresh_receipts_over_old_race_and_odds_imports(cloud):
    from hr_platform.cloud_normalization import normalize_next
    from test_realdata import race_archive

    async def scenario():
        old = cloud.seed(1, 0, kind='DAILY_SNAPSHOT')
        fresh = cloud.seed(2, 2, kind='DAILY_SNAPSHOT')
        race = cloud.seed(3, 1, race_archive(), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        latest_race = cloud.seed(4, 3, race_archive(), kind='NAR_RACE_BUNDLE', source='nar-daily-race')
        latest_odds = cloud.seed(5, 3, kind='DAILY_SNAPSHOT')
        completed = set()
        # Historical race bundles must not delay live odds; a receipt-time tie favors race state.
        for expected in (latest_race, latest_odds, fresh, race, old):
            assert (await normalize_next(cloud.bucket, cloud.db, None, 60,
                                        clock=lambda: cloud.clock[0]))['status'] == 'PARSED'
            actual = {r[0] for r in cloud.db.conn.execute(
                "SELECT observation_id FROM normalization_jobs WHERE status='DONE'")}
            assert actual - completed == {expected}
            completed = actual
    asyncio.run(scenario())


def seed_monthly(cloud, number, minute, raw=None, kind='FINAL_ONLY', filename='200001_0946680000_odds.zip'):
    from test_parser import monthly_archive
    event = cloud.seed(number, minute, monthly_archive() if raw is None else raw, kind, 'nar-daily-monthly')
    cloud.db.conn.execute('UPDATE captures SET file_name=? WHERE event_id=?', (filename, event))
    cloud.db.conn.commit()
    return event


def test_saved_monthly_reparse_is_final_only_and_never_backdated(cloud, monkeypatch):
    async def scenario():
        daily = cloud.seed(70, 0, kind='DAILY_SNAPSHOT')
        await cloud.h.normalize(daily)
        before = await cloud.h.asof(f.RACE, ['quinella'], f.at(5))
        final_event = seed_monthly(cloud, 71, 1)
        cloud.clock[0] = f.at(7)
        parsed = await cloud.h.normalize(final_event)
        assert parsed['version'] == module.FINAL_VERSION and parsed['status'] == 'OK'
        assert parsed['available_at'] == stamp(f.at(7))
        assert await cloud.h.normalize(final_event) == parsed
        assert await cloud.h.asof(f.RACE, ['quinella'], f.at(5)) == before
        assert (await cloud.h.asof(f.RACE, ['quinella'], f.at(7)))['markets']['quinella']['observation_id'] == daily
        assert (await cloud.h.final_prices(f.RACE, ['quinella'], f.at(6)))['missing_markets'] == ['quinella']
        current = await cloud.h.final_prices(f.RACE, ['quinella'], f.at(7))
        assert current['markets']['quinella']['observation_id'] == final_event
        assert not current['paper_eligible']
        assert current['markets']['quinella']['content']['source_updated_at'] is None
        monkeypatch.setattr(module, 'FINAL_VERSION', module.FINAL_VERSION + ':repair')
        cloud.clock[0] = f.at(9)
        repaired = await cloud.h.normalize(final_event)
        assert repaired['parse_id'] != parsed['parse_id'] and repaired['available_at'] == stamp(f.at(9))
        assert await cloud.h.final_prices(f.RACE, ['quinella'], f.at(7)) == current
        assert cloud.db.conn.execute('SELECT count(*) FROM raw_observations').fetchone()[0] == 2
    asyncio.run(scenario())


def test_monthly_normalization_does_not_delay_live_receipts_and_is_idempotent(cloud):
    from hr_platform.cloud_normalization import normalize_next
    async def scenario():
        final_event = seed_monthly(cloud, 73, 2)
        daily = cloud.seed(72, 0, kind='DAILY_SNAPSHOT')
        def run():
            return normalize_next(cloud.bucket, cloud.db, None, 60, clock=lambda: cloud.clock[0])
        assert (await run())['status'] == 'PARSED'
        rows = cloud.db.conn.execute('SELECT observation_id FROM normalization_jobs').fetchall()
        assert [r[0] for r in rows] == [daily]
        assert (await run())['status'] == 'PARSED'
        assert (await run())['status'] == 'IDLE'
        assert cloud.db.conn.execute('SELECT count(*) FROM odds_races WHERE parse_id IN (SELECT parse_id FROM odds_parses WHERE observation_id=?)', (final_event,)).fetchone()[0] == 2
    asyncio.run(scenario())


def test_invalid_late_monthly_member_publishes_no_partial_races(cloud):
    from test_parser import monthly_archive
    from hr_platform.parser import unzip
    body = next(iter(unzip(f.archive()).values()))
    raw = monthly_archive({'200001_01_odds.csv': body, '200001_02_odds.csv': b'SYNTHETIC changed header\n'})
    event = seed_monthly(cloud, 74, 0, raw)
    parsed = asyncio.run(cloud.h.normalize(event))
    assert parsed['status'] == 'ERROR' and parsed['available_at'] is None
    assert not cloud.db.conn.execute('SELECT * FROM odds_races').fetchall()
    assert (asyncio.run(cloud.h.final_prices(f.RACE, ['quinella'], f.at(5))))['missing_markets'] == ['quinella']


def test_monthly_observation_requires_final_only_kind(cloud):
    event = seed_monthly(cloud, 75, 0, kind='DAILY_SNAPSHOT')
    with pytest.raises(ValueError, match='MONTHLY_IS_FINAL_ONLY'):
        asyncio.run(cloud.h.normalize(event))


def test_monthly_aggregate_can_exceed_daily_limit_but_each_object_stays_bounded(cloud, monkeypatch):
    from test_parser import monthly_archive
    from hr_platform.common import canonical
    from hr_platform.parser import iter_monthly_odds_races, unzip
    races = dict(iter_monthly_odds_races(monthly_archive(), {}, '200001'))
    limit = max(len(canonical(v)) for v in races.values()) + 100
    monkeypatch.setattr(module, 'MAX_EXPANDED', limit)
    assert sum(len(canonical(v)) for v in races.values()) > limit
    event = seed_monthly(cloud, 76, 0)
    assert asyncio.run(cloud.h.normalize(event))['status'] == 'OK'
    assert all(len(body) <= limit for key, body in cloud.bucket.objects.items() if key.startswith('odds-normalized/'))
    # The same daily two-race archive retains its aggregate bound.
    body = next(iter(unzip(f.archive()).values()))
    combined = body + b''.join(body.splitlines(keepends=True)[1:]).replace(b'20000101', b'20000102')
    raw = monthly_archive({'20000101_odds.csv': combined})
    daily = cloud.seed(77, 0, raw, 'DAILY_SNAPSHOT')
    assert asyncio.run(cloud.h.normalize(daily))['status'] == 'ERROR'
