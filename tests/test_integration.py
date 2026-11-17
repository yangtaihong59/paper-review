from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from paper_review.config import load
from paper_review.server import create_app

ROOT = Path(__file__).resolve().parents[1]


def test_demo_config_and_review(tmp_path):
    cfg = load(ROOT / 'config.toml')
    paper = cfg.paper('example')
    assert paper.sync_tool.is_file()
    with TestClient(create_app(cfg, paper.id, tmp_path), base_url='http://127.0.0.1:8790') as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/papers', headers={'Host': 'evil.example'}).status_code == 403
        doc = client.get('/api/papers/example/document').json()
        assert any(row.get('id') == 'intro-01' and row.get('zh') and row.get('en') for row in doc['rows'])
        assert client.post('/api/papers/example/annotations', json={'para_id': 'intro-01', 'text': 'test'}).status_code == 403
        response = client.post('/api/papers/example/annotations', json={'para_id': 'intro-01', 'text': 'test'}, headers={'X-Paper-Review': '1'})
        assert response.status_code == 200


def test_non_loopback_config_rejected(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[server]\nhost = "0.0.0.0"\n')
    with pytest.raises(ValueError):
        load(path)
