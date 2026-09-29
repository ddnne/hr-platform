"""One daily race ZIP observation, distributed to races only when reading it.

Receipt times establish observation; neither file time nor empty result fields
establish the provider's update time or an active runner set.
"""

from datetime import datetime
import re
from urllib.parse import parse_qs, urlsplit
from .common import identity
from .parser import MAX_COMPRESSED
from .race_files import parse_race_bundle, VERSION
from .race_state import StateEvidence, validate_body_times
from .realdata import filename_metadata


def parse_metadata(raw, date):
    bundle = parse_race_bundle(raw)
    if any(key.split(":")[0] != date for key in bundle["races"]):
        raise ValueError("METADATA_DATE")
    return {"races": bundle["races"], "source_updated_at": None}


class MetadataEvidence(StateEvidence):
    table_prefix = "race_metadata"
    scope_key = "date"
    version = VERSION + ":receipt-v1:utf-8-sig"
    parser = staticmethod(parse_metadata)

    def validate(self, receipt, raw, date, now):
        started = validate_body_times(receipt, raw, now, MAX_COMPRESSED)
        datetime.strptime(date, "%Y%m%d")
        url = urlsplit(receipt["url"])
        if (url.scheme != "https" or url.netloc != "www.keiba.go.jp" or url.fragment
            or url.path != "/KeibaWeb/DataDownload/RaceDataDownload"
            or parse_qs(url.query, strict_parsing=True) != {"type": ["daily"]}
            or not re.fullmatch(date + r"_\d{10}_race\.zip", receipt.get("filename", ""))):
            raise ValueError("METADATA_RECEIPT_URL_OR_FILENAME")
        return identity([receipt["url"], started])

    def verify_parsed(self, parsed, receipt):
        parsed.update(filename_metadata(receipt["filename"]))

    def for_race(self, race_id, at):
        result = self.asof(race_id.split(":")[0], at)
        evidence = result["evidence"]
        # Resolve the newest observation/parse before selecting a race. A removed
        # race must not reappear from a previous version of the same daily ZIP.
        if evidence:
            evidence = {k: v for k, v in evidence.items() if k != "races"}
            evidence["metadata"] = result["evidence"].get("races", {}).get(race_id)
        return {"race_id": race_id, "asof_at": result["asof_at"],
                "evidence": evidence, "age_seconds": result["age_seconds"]}
