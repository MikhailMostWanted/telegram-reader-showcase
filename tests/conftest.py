import pytest
from telegram_reader.core import Settings


@pytest.fixture
def settings():
    return Settings(public_url="https://reader.example.com", api_id=12345, api_hash="a" * 32,
                    github_client_id="test-client", github_client_secret="synthetic-secret",
                    github_owner_id=2468013579, signing_key="x" * 48, telegram_user_id=99,
                    allow_all_chats=True)
