import pytest


@pytest.fixture(autouse=True)
def isolated_rehearsal_configuration(monkeypatch):
    # Unit tests explicitly opt into fixtures. Application defaults remain live AI + HTTP merchant.
    monkeypatch.setenv('REWIND_MERCHANT', 'inline')
    monkeypatch.setenv('REWIND_AI', 'fixture')
    monkeypatch.setenv('REWIND_PROVIDER', 'emulator')
    monkeypatch.setenv('REWIND_CHAOS', 'true')
