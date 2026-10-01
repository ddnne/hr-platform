"""Cloud race-list/result snapshots. Uses the same R2/D1 access and CSV parser as odds."""
import csv
import json
import re
from .cloud_history import CloudHistory, PUBLICATION_CLOCK
from .common import canonical, identity, sha, stamp
from .parser import MAX_COMPRESSED, MAX_EXPANDED
from .race_files import parse_race_bundle, VERSION as PARSER_VERSION

VERSION = f'cloud-race-files-v1:{PARSER_VERSION}'


class CloudRaceFiles(CloudHistory):
    body_prefix = 'race-normalized'
    parse_table = 'race_file_parses'
    races_table = 'race_file_races'

    async def normalize(self, observation_id, encoding='utf-8-sig'):
        if not isinstance(observation_id, str) or not re.fullmatch(
            r'nar-(?:daily-race:\d+|mac-import:[0-9a-f]{64})', observation_id,
        ):
            raise ValueError('OBSERVATION_ID')
        if encoding not in {'utf-8-sig', 'cp932'}:
            raise ValueError('ENCODING_UNQUALIFIED')
        parse_id = identity([observation_id, VERSION, encoding])
        old = await self.first('SELECT * FROM race_file_parses WHERE parse_id=?', parse_id)
        if old and (old['available_at'] or old['status'] == 'ERROR'):
            return old
        obs = await self.first('SELECT * FROM raw_observations WHERE observation_id=?', observation_id)
        if not obs or obs['dataset_kind'] not in {'NAR_RACE_BUNDLE', 'SYNTHETIC'}:
            raise ValueError('OBSERVATION_MISSING_OR_KIND')
        if stamp(obs['raw_saved_at']) > stamp(self.clock()):
            raise ValueError('CLOCK_ORDER')
        if not re.fullmatch(r'[0-9a-f]{64}', obs['raw_sha256']):
            raise ValueError('RAW_HASH')
        raw = await self.body(f"raw/{obs['raw_sha256']}", MAX_COMPRESSED)
        if sha(raw) != obs['raw_sha256']:
            raise ValueError('BODY_CORRUPT')
        try:
            bundle = parse_race_bundle(raw, encoding)
            races, total = {}, 0
            for race_id, race in bundle['races'].items():
                # Neither an expected close nor an empty result is an observed sales status.
                race = {**race, 'sales_close_at': None, 'source_updated_at': None}
                data = canonical(race)
                total += len(data)
                if total > MAX_EXPANDED:
                    raise ValueError('NORMALIZED_LIMIT')
                races[race_id] = {'body_hash': await self.save_body(data), 'markets': []}
            digest = await self.save_body(canonical({'format': 'race-files-v1', 'races': races}))
        except (ValueError, UnicodeError, KeyError, csv.Error) as exc:
            await self.run("INSERT OR IGNORE INTO race_file_parses VALUES(?,?,?,?,'ERROR',?,NULL,NULL,?)",
                           parse_id, observation_id, VERSION, encoding, stamp(self.clock()), type(exc).__name__)
            return await self.first('SELECT * FROM race_file_parses WHERE parse_id=?', parse_id)
        await self.run("INSERT OR IGNORE INTO race_file_parses VALUES(?,?,?,?,'WRITING',?,NULL,?,NULL)",
                       parse_id, observation_id, VERSION, encoding, stamp(self.clock()), digest)
        stored = await self.first('SELECT * FROM race_file_parses WHERE parse_id=?', parse_id)
        if stored['body_hash'] != digest:
            raise ValueError('PARSE_VERSION_CONFLICT')
        await self.run("""INSERT OR IGNORE INTO race_file_races
            SELECT ?,value,'[]' FROM json_each(?)""", parse_id, json.dumps(list(races)))
        await self.run(f"""UPDATE race_file_parses SET status='OK',available_at={PUBLICATION_CLOCK}
            WHERE parse_id=? AND available_at IS NULL AND parsed_at<={PUBLICATION_CLOCK}""", parse_id)
        result = await self.first('SELECT * FROM race_file_parses WHERE parse_id=?', parse_id)
        if result['available_at'] is None:
            raise ValueError('CLOCK_ORDER')
        return result

    async def day(self, date, at, race_id=None):
        if not isinstance(date, str) or not re.fullmatch(r'\d{8}', date):
            raise ValueError('RACE_DATE')
        if race_id is not None and (not isinstance(race_id, str) or race_id.split(':')[0] != date):
            raise ValueError('RACE_DATE_MISMATCH')
        cutoff = self.cutoff(at)
        row = await self.first('''SELECT p.*,o.received_at,o.raw_saved_at,o.raw_sha256
            FROM race_file_parses p JOIN raw_observations o USING(observation_id)
            WHERE p.status='OK' AND p.available_at<=?
              AND NOT EXISTS (SELECT 1 FROM race_file_parses newer
                WHERE newer.observation_id=p.observation_id AND newer.status='OK' AND newer.available_at<=?
                AND (newer.available_at,newer.parse_id)>(p.available_at,p.parse_id))
              AND EXISTS (SELECT 1 FROM race_file_races r WHERE r.parse_id=p.parse_id AND substr(r.race_id,1,8)=?)
            ORDER BY o.received_at DESC,p.available_at DESC,p.parse_id DESC LIMIT 1''', cutoff, cutoff, date)
        if not row:
            return {'asof_at': cutoff, 'snapshot': None, 'paper_eligible': False}
        index = await self.read_body(row['body_hash'])
        races = {key: await self.read_body(value['body_hash']) for key, value in index['races'].items()
                 if key.split(':')[0] == date and (race_id is None or key == race_id)}
        return {'asof_at': cutoff, 'snapshot': row, 'races': races, 'paper_eligible': False,
                'availability_clock': 'D1_PUBLICATION_STATEMENT_UTC'}
