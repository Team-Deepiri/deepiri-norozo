from pathlib import Path

import pytest

import setup


@pytest.mark.asyncio
async def test_fetch_repo_and_categories_parses_ids(monkeypatch):
    async def fake_graphql(query, variables, pat, retries=2, timeout_seconds=20.0):
        return {
            "data": {
                "repository": {
                    "id": "R_123",
                    "hasDiscussionsEnabled": True,
                    "discussionCategories": {
                        "nodes": [
                            {"id": "C_1", "name": "General"},
                            {"id": "C_2", "name": "Announcements"},
                        ]
                    },
                }
            }
        }

    monkeypatch.setattr(setup, "_graphql_request", fake_graphql)

    repo_id, categories, has_discussions_enabled = await setup.fetch_repo_and_categories("owner", "repo", "pat")

    assert repo_id == "R_123"
    assert has_discussions_enabled is True
    assert categories[1]["id"] == "C_2"
    assert setup.pick_category_id(categories, "Announcements") == "C_2"


def test_upsert_env_file_updates_and_appends(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("FOO=bar\nREPO_ID=old\n", encoding="utf-8")

    setup._upsert_env_file(env_path, {"REPO_ID": "new_repo", "CATEGORY_ID": "cat_1"})

    text = env_path.read_text(encoding="utf-8")
    assert "REPO_ID=new_repo" in text
    assert "CATEGORY_ID=cat_1" in text
    assert "FOO=bar" in text
