"""Retired exchange features cannot be re-enabled by stale clients or profiles."""
import json
from dataclasses import asdict
from pathlib import Path

from fastapi.testclient import TestClient
from test_app_lifecycle_settings import _isolated_app

from deepdesk.provider_secrets import ProviderSecrets, ProviderSecretsStore
from deepdesk.runtime_settings import RuntimeSettings, RuntimeSettingsPatch, RuntimeSettingsStore
from deepdesk.secret_storage import AesGcmProtector, LocalSecretVault


def test_finance_routes_tools_and_settings_are_absent(tmp_path, monkeypatch):
    app = _isolated_app(tmp_path, monkeypatch)
    assert {'okx_finance', 'binance_finance'}.isdisjoint(app.state.registry.health()['names'])
    assert not any(hasattr(app.state, name) for name in (
        'finance_policy', 'okx_monitor', 'okx_client', 'binance_client',
    ))
    with TestClient(app) as client:
        for path in ('/api/finance/status', '/api/finance/market?refresh=true',
                     '/api/okx/status', '/api/okx/market', '/api/binance/status'):
            assert client.get(path).status_code == 404
        for path in ('/api/settings', '/api/status'):
            result = client.get(path)
            assert result.status_code == 200
            assert not any(token in key.lower() for key in result.json()
                           for token in ('finance', 'okx', 'binance'))
        for field, value in {'finance_enabled': True, 'finance_exchange': 'all',
                             'finance_watchlist': 'ALL', 'okx_api_key': 'synthetic-retired',
                             'binance_secret_key': 'synthetic-retired'}.items():
            assert client.patch('/api/settings', json={field: value}).status_code == 422
        assert client.patch('/api/settings', json={'request_timeout': 42}).status_code == 200
    assert not (tmp_path / 'data/crypto-market-snapshot.json').exists()


def test_old_runtime_settings_keep_supported_preferences(tmp_path):
    path = tmp_path / 'runtime-settings.json'
    path.write_text(json.dumps({'finance_enabled': True, 'finance_exchange': 'okx',
                                'finance_watchlist': 'ALL', 'voice_rate': 1.25,
                                'request_timeout': 42}), encoding='utf-8')
    store = RuntimeSettingsStore(path, RuntimeSettings())
    assert store.value.voice_rate == 1.25
    assert store.value.request_timeout == 42
    assert not any('finance' in key for key in store.value.model_dump())
    store.update(RuntimeSettingsPatch(request_timeout=43))
    assert 'finance' not in path.read_text(encoding='utf-8')


def test_old_encrypted_vault_drops_exchange_keys_preserves_other_providers(tmp_path):
    path = tmp_path / 'provider-secrets.vault'
    protector = AesGcmProtector('test-aes', b'k' * 32)
    vault = LocalSecretVault(path, protector=protector)
    vault.write_verified({'okx_api_key': 'synthetic-retired',
                          'binance_secret_key': 'synthetic-retired',
                          'deepseek_primary': 'synthetic-retained',
                          'telegram_bot_token': 'synthetic-channel'})
    store = ProviderSecretsStore(path, ProviderSecrets(), protector=protector)
    assert store.value.deepseek_primary == 'synthetic-retained'
    assert store.value.telegram_bot_token == 'synthetic-channel'
    assert not any(token in key for key in asdict(store.value)
                   for token in ('finance', 'okx', 'binance'))
    assert 'okx_api_key' not in vault.read().values
    assert 'binance_secret_key' not in vault.read().values


def test_shipping_ui_has_no_retired_exchange_surface():
    root = Path(__file__).resolve().parents[1]
    for name in ('index.html', 'app.js', 'history.css', 'professional-ui.css'):
        source = (root / 'deepdesk/static' / name).read_text(encoding='utf-8').lower()
        assert not any(token in source for token in ('finance', 'financial trading', 'okx', 'binance', '金融'))
    assert not (root / 'deepdesk/finance_policy.py').exists()
    assert not (root / 'deepdesk/plugins/builtin/okx_finance.py').exists()
    assert not (root / 'deepdesk/plugins/builtin/binance_finance.py').exists()
