import json
import tempfile
import unittest
from pathlib import Path

from xay.core import open_db, append, rebuild
from xay.core.errors import PermissionError
from xay.core.snapshot import build

ROOT = Path(__file__).parents[1] / 'fixtures' / 'golden'


def value_at(obj, path):
    import re
    cur = obj
    for part in re.findall(r'[^.\[\]]+|\[\d+\]', path):
        if part.startswith('['):
            cur = cur[int(part[1:-1])]
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = None
    return cur


class GoldenTests(unittest.TestCase):
    def test_all_samples_and_rebuild(self):
        for event_file in sorted(ROOT.glob('*.events.jsonl')):
            with self.subTest(sample=event_file.name), tempfile.TemporaryDirectory() as tmp:
                conn = open_db(str(Path(tmp) / 'sample.db'))
                events = [json.loads(line) for line in event_file.read_text().splitlines()]
                expected = json.loads(event_file.with_name(event_file.name.replace('.events.jsonl', '.expect.json')).read_text())
                for number, event in enumerate(events, 1):
                    err = next((x for x in expected if x.get('path') == '$error' and x.get('line') == number), None)
                    try:
                        append(conn, event['type'], event['by'], event['data'], ws=event.get('ws'), conv=event.get('conv'), subject=event.get('subject'), dedup=event.get('dedup'), ts=event['ts'])
                        if event.get('v', 1) != 1:
                            conn.execute('UPDATE events SET v=? WHERE seq=(SELECT MAX(seq) FROM events)', (event['v'],))
                            rebuild(conn)
                    except PermissionError:
                        if not err:
                            raise
                    else:
                        self.assertIsNone(err, f'unexpected success on line {number}')
                snap = build(conn, ws='ws_demo01', conv=f"cv_demo{event_file.name[:2]}", now=events[-1]['ts'])
                for check in expected:
                    if check['path'] == '$error':
                        continue
                    self.assertEqual(value_at(snap, check['path']), check['equals'], check['path'])
                before = json.dumps(snap, ensure_ascii=False, sort_keys=True)
                rebuild(conn)
                after = json.dumps(build(conn, ws='ws_demo01', conv=f"cv_demo{event_file.name[:2]}", now=events[-1]['ts']), ensure_ascii=False, sort_keys=True)
                self.assertEqual(before, after)
                conn.close()


if __name__ == '__main__':
    unittest.main()
