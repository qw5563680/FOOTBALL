"""Raw inventory for later B/C work; deliberately does not modify the registry."""

import csv
import hashlib
import json
from pathlib import Path

REPO = Path('/workspace/work/football-t65')
ARCHIVE = Path('/workspace/work/t65-fixed-archive')
OUT = Path('/workspace/work/t65-evidence/mappings')
SOURCE = 'state/catalogue/43539439430-1/apif-raw.json'
COMMIT = 'e5848e5d7de83a0cd024fa3e03e08cf6db6c7ee4'


def main():
    raw = json.loads((ARCHIVE / SOURCE).read_text())
    registry = json.loads((REPO / 'data/league_map.json').read_text())
    included = set(registry['providers']['apif'])
    countries = {row['country'].replace('South Korea', 'South-Korea')
                 for row in registry['supporting_scope']['countries']}
    # These named national competitions were individually read from raw metadata.
    # Their IDs are listed only for next-task inspection, never as approved routes.
    national_ids = {
        29, 31, 33, 35, 36, 536, 808, 858, 19, 1163, 806, 24, 28, 25,
        21, 535, 849, 859, 860, 913, 1008, 1169, 766,
    }
    entries = []
    for index, entry in enumerate(raw['response']):
        code = entry['league']['id']
        country = entry['country']['name']
        if str(code) in included:
            continue
        if country in countries:
            scope = 'B-country-inventory'
            reason = 'Country identified by a master domestic cup; exact division, sex/age population and historical relevance require next-task review. No import proposal or approval.'
        elif code in national_ids:
            scope = 'C-national-inventory'
            reason = 'Additional national-team championship/qualifier/nations league/invitational named in raw catalogue; later task must decide relevance and identities. No import proposal or approval.'
        else:
            continue
        entries.append({
            'scope': scope, 'id': code, 'name': entry['league']['name'], 'country': country,
            'type': entry['league']['type'], 'catalogue_seasons': [season['year'] for season in entry['seasons']],
            'review': 'next_task_unreviewed', 'reason': reason, 'data_commit': COMMIT,
            'path': SOURCE, 'sha256': hashlib.sha256((ARCHIVE / SOURCE).read_bytes()).hexdigest(),
            'pointer': f'/response/{index}',
        })
    artifact = {
        'purpose': 'Next-task inventory only; entries have not been added to providers or approved mappings.',
        'known_B_country_scope': sorted(countries),
        'B_limitation': 'The complete domestic catalogue for each known cup country is a worklist. Entries may be women/youth/regional and need explicit population and relevance review. It is not an adult-male route list.',
        'participant_country_gap': 'A continental/club tournament league catalogue does not enumerate participant clubs/countries. The remaining B countries must be enumerated from actual season participants in the next task; none are silently excluded.',
        'C_limitation': 'Additional adult national competitions only. Other youth/women supporting competitions require separate identities and a dedicated next-task inventory; they are not silently merged into adult men.',
        'B_entries': sum(entry['scope'].startswith('B-') for entry in entries),
        'C_entries': sum(entry['scope'].startswith('C-') for entry in entries),
        'entries': entries,
    }
    (OUT / 'next-task-supporting-scope.json').write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + '\n')
    with (OUT / 'next-task-supporting-scope.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=entries[0].keys())
        writer.writeheader()
        for entry in entries:
            writer.writerow({**entry, 'catalogue_seasons': ','.join(map(str, entry['catalogue_seasons']))})
    print(json.dumps({key: artifact[key] for key in ('B_entries', 'C_entries', 'known_B_country_scope')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
