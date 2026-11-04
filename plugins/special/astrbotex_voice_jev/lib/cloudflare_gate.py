"""Compatibility import for older local Cloudflare configurations."""
import re
import urllib.request
from decision_client import DecisionClient, GateError, NoRedirect, windows_secret


class ClefClient(DecisionClient):
    def __init__(self, config, root=None):
        config = dict(config)
        account = str(config.pop('account_id', ''))
        model = config.get('model', 'clef')
        if not re.fullmatch(r'[0-9a-f]{32}', account) or model not in ('clef', 'clef-flash', 'clef-omni'):
            raise GateError('Cloudflare account_id or model is invalid')
        config.update({'type': 'cloudflare', 'model': model,
                       'url': 'https://api.cloudflare.com/client/v4/accounts/{}/ai/run/@cf/cloudflare/{}'.format(account, model)})
        config.setdefault('token_env', 'CLOUDFLARE_API_TOKEN')
        super().__init__(config, root)
