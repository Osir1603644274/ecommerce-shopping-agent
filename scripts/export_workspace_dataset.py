"""Export private, UNREVIEWED dialogue records; never publish or create qrels."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.app.workspace_archive import WorkspaceArchive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--name', required=True, help='New private JSONL filename, without directories')
    args = parser.parse_args()
    if not __import__('re').fullmatch(r'[a-zA-Z0-9_-]+\.jsonl', args.name):
        parser.error('Use a simple .jsonl filename')
    archive = WorkspaceArchive()
    folder = archive.path.parent / 'exports'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / args.name
    # Exclusive creation: earlier snapshots remain immutable.
    with sqlite3.connect(f'{archive.path.as_uri()}?mode=ro', uri=True) as db, target.open('x', encoding='utf-8') as out:
        db.row_factory = sqlite3.Row
        count = 0
        for row in db.execute('SELECT * FROM workspace_messages ORDER BY seq'):
            message = json.loads(row['payload_json'])
            record = dict(schemaVersion='workspace-dialogue-dataset-v1', ownerHash=row['owner'],
                conversationId=row['conversation_id'], requestId=row['request_id'], role=row['role'],
                capturedAt=row['captured_at'], source=row['source'], message=message,
                originalContentSha256=row['original_sha256'],
                redactionApplied=hashlib.sha256(message['content'].encode()).hexdigest() != row['original_sha256'],
                labelingStatus='UNREVIEWED', benchmarkEligible=False)
            out.write(json.dumps(record, ensure_ascii=False) + '\n')
            count += 1
    print(json.dumps(dict(path=str(target), messages=count, sha256=hashlib.sha256(target.read_bytes()).hexdigest())))


if __name__ == '__main__':
    main()
